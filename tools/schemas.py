"""
Shared data contracts for TripWeaver.

These schemas are the single source of truth for the shape of data moving
between tools, agents, the MCP server, and the API layer. Define changes
here first — everything downstream depends on these.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class CabinClass(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class FlightSearchInput(BaseModel):
    """Input contract for search_flights()."""

    origin: str = Field(..., min_length=3, max_length=3, description="IATA airport/city code")
    destination: str = Field(..., min_length=3, max_length=3, description="IATA airport/city code")
    depart_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    cabin_class: CabinClass = CabinClass.ECONOMY
    max_budget_usd: float | None = Field(default=None, gt=0)

    @field_validator("origin", "destination")
    @classmethod
    def uppercase_iata(cls, v: str) -> str:
        return v.upper()

    @field_validator("return_date")
    @classmethod
    def return_after_depart(cls, v: date | None, info) -> date | None:
        depart = info.data.get("depart_date")
        if v is not None and depart is not None and v < depart:
            raise ValueError("return_date cannot be before depart_date")
        return v


class FlightSegment(BaseModel):
    """A single flown leg (e.g. one takeoff/landing) within an offer."""

    carrier_iata: str
    carrier_name: str
    flight_number: str
    origin_iata: str
    destination_iata: str
    departs_at: datetime
    arrives_at: datetime


class FlightOffer(BaseModel):
    """Output contract: one bookable flight offer."""

    offer_id: str
    total_price_usd: float
    price_currency_original: str = "USD"
    cabin_class: CabinClass
    stops_outbound: int
    segments: list[FlightSegment]
    expires_at: datetime | None = Field(
        default=None, description="Offers expire; re-fetch after this time before booking"
    )


class FlightSearchResult(BaseModel):
    """Output contract for search_flights()."""

    query: FlightSearchInput
    offers: list[FlightOffer]
    provider: str = "duffel"
    is_sandbox: bool = True


class ToolError(BaseModel):
    """Standard error shape returned by tools instead of raising raw exceptions
    up to the agent layer — lets an agent reason about failure instead of crashing."""

    tool_name: str
    error_type: str
    message: str
    retryable: bool = False
