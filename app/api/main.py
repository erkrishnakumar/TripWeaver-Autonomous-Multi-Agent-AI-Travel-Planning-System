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
from typing import Annotated, NoReturn

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.api.schemas import (
    ApprovalDecisionResponse,
    BookingRead,
    ConfirmApprovalRequest,
    RejectApprovalRequest,
    TripCreate,
    TripProceedResponse,
    TripRead,
)
from app.db.models import Booking, Trip
from app.db.models.enums import TripStatus
from app.tools.confirm_booking import confirm_booking, reject_booking
from app.tools.create_trip import create_trip
from app.tools.schemas import ToolError
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


@app.get("/trips/{trip_id}/bookings", response_model=list[BookingRead])
async def list_trip_bookings(
    trip_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[BookingRead]:
    """What a human approver actually looks at: every Booking proposed for
    this trip, with its Approval decision inlined, so approval_id (needed
    for POST /approvals/{id}/confirm|reject) never has to be dug out of the
    database directly."""
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    result = await db.execute(
        select(Booking).where(Booking.trip_id == trip_id).options(selectinload(Booking.approval))
    )
    bookings = result.scalars().all()
    read_rows = []
    for booking in bookings:
        assert booking.approval is not None  # propose_booking() always creates one
        read_rows.append(
            BookingRead(
                booking_id=booking.id,
                booking_type=booking.booking_type,
                status=booking.status,
                total_price_usd=booking.total_price_usd,
                provider_booking_reference=booking.provider_booking_reference,
                failure_reason=booking.failure_reason,
                approval_id=booking.approval.id,
                approval_decision=booking.approval.decision,
            )
        )
    return read_rows


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


_NOT_FOUND_ERROR_TYPES = {"approval_not_found", "booking_not_found"}
_CONFLICT_ERROR_TYPES = {"already_decided", "booking_not_pending"}


def _raise_for_tool_error(error: ToolError) -> NoReturn:
    """Gate 2's two endpoints share one error contract (see confirm_booking.
    py's _load_approval_and_booking()) -- not found / already-decided /
    everything else map to 404 / 409 / 422 respectively, same reasoning as
    the existing /trips/{id}/proceed 409 for a trip in the wrong state."""
    if error.error_type in _NOT_FOUND_ERROR_TYPES:
        raise HTTPException(status_code=404, detail=error.message)
    if error.error_type in _CONFLICT_ERROR_TYPES:
        raise HTTPException(status_code=409, detail=error.message)
    raise HTTPException(status_code=422, detail=error.message)


@app.post("/approvals/{approval_id}/confirm", response_model=ApprovalDecisionResponse)
async def confirm_approval(
    approval_id: uuid.UUID,
    body: ConfirmApprovalRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalDecisionResponse:
    """Gate 2. THE ONLY endpoint in this project ever allowed to trigger a
    real provider booking -- see app/tools/confirm_booking.py's module
    docstring for the full contract, including why a provider-side failure
    comes back as a normal 200 (booking_status="booking_failed") rather
    than an HTTP error: the human's approval succeeded, Duffel's booking
    call is a separate, honestly-reported outcome."""
    result = await confirm_booking(
        db,
        str(approval_id),
        passengers=body.passengers,
        guests=body.guests,
        contact_email=body.contact_email,
        contact_phone_number=body.contact_phone_number,
        decided_by=body.decided_by,
    )
    if isinstance(result, ToolError):
        _raise_for_tool_error(result)
    return ApprovalDecisionResponse(
        booking_id=uuid.UUID(result.booking_id),
        approval_id=uuid.UUID(result.approval_id),
        booking_status=result.booking_status,
        provider_booking_reference=result.provider_booking_reference,
        message=result.message,
    )


@app.post("/approvals/{approval_id}/reject", response_model=ApprovalDecisionResponse)
async def reject_approval(
    approval_id: uuid.UUID,
    body: RejectApprovalRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalDecisionResponse:
    result = await reject_booking(
        db,
        str(approval_id),
        decided_by=body.decided_by,
        decision_notes=body.decision_notes,
    )
    if isinstance(result, ToolError):
        _raise_for_tool_error(result)
    return ApprovalDecisionResponse(
        booking_id=uuid.UUID(result.booking_id),
        approval_id=uuid.UUID(result.approval_id),
        booking_status=result.booking_status,
        provider_booking_reference=result.provider_booking_reference,
        message=result.message,
    )
