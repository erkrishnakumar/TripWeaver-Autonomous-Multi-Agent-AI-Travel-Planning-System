"""
Pydantic response models for the API layer — never return a SQLAlchemy
ORM object directly from an endpoint; this is the explicit, versioned
boundary between the DB schema and what a client actually sees.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import ApprovalDecision, BookingStatus, BookingType, TripStatus
from app.tools.schemas import HotelGuestDetails, PassengerDetails


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


class ConfirmApprovalRequest(BaseModel):
    """Body for POST /approvals/{id}/confirm — Gate 2.

    This is the first and only point in the entire API where real
    passenger/guest PII is ever submitted; see confirm_booking()'s own
    docstring for why it is never collected or persisted any earlier.
    Provide `passengers` for a flight booking, or `guests` +
    `contact_email` + `contact_phone_number` for a hotel booking — whichever
    matches the booking this approval is for. Car rental approvals cannot
    be confirmed yet (see docs/Car_Rental_Payment_Gap.md).
    """

    passengers: list[PassengerDetails] | None = None
    guests: list[HotelGuestDetails] | None = None
    contact_email: str | None = None
    contact_phone_number: str | None = None
    decided_by: str | None = None


class RejectApprovalRequest(BaseModel):
    decided_by: str | None = None
    decision_notes: str | None = None


class ApprovalDecisionResponse(BaseModel):
    booking_id: uuid.UUID
    approval_id: uuid.UUID
    booking_status: str
    provider_booking_reference: str | None = None
    message: str


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8)
    full_name: str | None = Field(None, min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: str


class ForgotPasswordResponse(BaseModel):
    """message is always the same regardless of whether the email matched
    a real account, or whether the send actually succeeded -- both are
    real user-enumeration vectors otherwise (see forgot_password()'s own
    docstring in app/api/main.py).

    reset_token is populated ONLY as a dev-mode fallback when
    RESEND_API_KEY isn't configured at all (see docs/Auth_Requirement.md)
    -- when real email delivery is configured, this field is always null,
    since returning it here would mean anyone who can call this endpoint
    can reset anyone's password."""

    message: str
    reset_token: str | None = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None
    is_active: bool


class AuditLogEntryRead(BaseModel):
    """One event from a trip's audit trail -- the same append-only record
    app/tools/audit.py's log_stage_event() has always written (research_
    started/completed/failed, booking.proposed/confirmed/rejected/failed,
    etc.). This is the first place any of it is exposed over HTTP; before
    this endpoint, seeing a trip's history meant a raw DB query."""

    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_type: str
    payload: dict[str, Any]
    booking_id: uuid.UUID | None
    created_at: datetime


class BookingRead(BaseModel):
    """One proposed (or since-decided) booking on a trip, with its approval
    decision inlined — this is what a human approver looks at to decide what
    to pass to POST /approvals/{id}/confirm or /reject. approval_id is
    always present: propose_booking() never creates a Booking without one
    (see app/tools/propose_booking.py)."""

    model_config = ConfigDict(from_attributes=True)

    booking_id: uuid.UUID
    booking_type: BookingType
    status: BookingStatus
    total_price_usd: float
    provider_booking_reference: str | None
    failure_reason: str | None
    approval_id: uuid.UUID
    approval_decision: ApprovalDecision


class ConfirmInfoResponse(BaseModel):
    """What GET /trips/{id}/bookings alone can't tell an approver: the
    real, provider-generated ids needed to actually fill out POST
    /approvals/{id}/confirm's body. Before this endpoint existed, getting
    a flight booking's real passenger_ids meant a one-off DB script (see
    docs/TripWeaver_Roadmap.md's open item on this) -- passenger_ids here
    are LIVE re-fetched from the provider (same hallucination-guard
    discipline confirm_booking() itself uses), not read from a
    possibly-stale cached value, so they're guaranteed valid to use
    immediately."""

    booking_type: BookingType
    approval_id: uuid.UUID
    passenger_ids: list[str] | None = None
    note: str
