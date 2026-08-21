"""
propose_booking() — the approval-gate tool.

This tool writes a Booking row in PENDING_APPROVAL status and an associated
Approval row in PENDING status. It NEVER calls a real provider booking
endpoint. This is a permanent architectural property, not a current
limitation — the only path that is ever allowed to make a real booking is a
separate, explicitly human-triggered confirm endpoint (Phase 8), which does
not exist in this file and should never be added here.

Unlike the other tools in app/tools/, this one is DB-backed rather than
HTTP-backed, and is async to match app/db/session.py's async engine. It's
still deterministic and typed in the same spirit: given the same offer and
trip, its job (persist a pending proposal) never varies based on external
API behavior, the way search_flights()/search_hotels() do.

ONE TRANSACTION, ONE CALL: persisting the FlightOption/HotelOption row,
the Booking row, the Approval row, and an AuditLog row all happen together
and are committed atomically. If any step fails, nothing is left half
-written — there is no state where a Booking exists without its Approval,
or where an option row is orphaned without a Booking referencing it.

IDEMPOTENCY: the idempotency_key is deterministically derived from
(trip_id, provider_offer_id, booking_type) via a hash, matching the
Booking model's uq_bookings_idempotency_key unique constraint. Calling
propose_booking() twice for the same offer on the same trip does not
create a duplicate pending booking — it returns the existing one, with
was_existing=True on the result, so a retried agent call or a doubled UI
click is safe rather than silently multiplying pending bookings.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, Booking, CarRentalOption, FlightOption, HotelOption
from app.db.models.enums import ApprovalDecision, BookingStatus, BookingType
from app.tools.schemas import (
    ProposeBookingResult,
    ProposeCarBookingInput,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)


def _make_idempotency_key(trip_id: str, provider_offer_id: str, booking_type: BookingType) -> str:
    """Deterministic — same inputs always produce the same key, so a retry
    naturally lands on the same row instead of needing external
    coordination to avoid duplicates."""
    raw = f"{trip_id}:{provider_offer_id}:{booking_type.value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


async def _find_existing_booking(session: AsyncSession, idempotency_key: str) -> Booking | None:
    result = await session.execute(
        select(Booking).where(Booking.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def _write_audit_log(
    session: AsyncSession,
    *,
    trip_id: uuid.UUID,
    booking_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    from app.db.models import AuditLog

    session.add(
        AuditLog(
            id=uuid.uuid4(),
            trip_id=trip_id,
            booking_id=booking_id,
            event_type=event_type,
            payload=payload,
        )
    )


async def propose_booking(
    session: AsyncSession,
    query: ProposeFlightBookingInput | ProposeHotelBookingInput | ProposeCarBookingInput,
) -> ProposeBookingResult | ToolError:
    """
    Propose a booking for human approval. Writes PENDING_APPROVAL — never
    books anything real. See module docstring for the full contract.

    The caller owns the transaction boundary: this function adds rows and
    flushes so IDs are available, but does not commit. Callers should
    commit (typically via the same `async with get_session()` block this
    was called inside) after calling this, so a failure elsewhere in a
    larger flow can still roll back the proposal atomically.
    """
    try:
        trip_uuid = uuid.UUID(query.trip_id)
    except ValueError:
        return ToolError(
            tool_name="propose_booking",
            error_type="invalid_trip_id",
            message=f"'{query.trip_id}' is not a valid UUID.",
            retryable=False,
        )

    if isinstance(query, ProposeFlightBookingInput):
        booking_type = BookingType.FLIGHT
        provider_offer_id = query.offer.offer_id
        total_price_usd = query.offer.total_price_usd
    elif isinstance(query, ProposeHotelBookingInput):
        booking_type = BookingType.HOTEL
        provider_offer_id = query.listing.search_result_id
        total_price_usd = query.listing.estimated_price_total_usd
    else:
        booking_type = BookingType.CAR
        # Keyed on the FIRM quote_id, not the original rate_id — a quote is
        # what's actually being proposed for booking, and re-quoting the
        # same rate later would otherwise collide on the same idempotency
        # key despite being a distinct proposal.
        provider_offer_id = query.quote.quote_id
        total_price_usd = query.quote.total_price_usd

    idempotency_key = _make_idempotency_key(query.trip_id, provider_offer_id, booking_type)

    existing = await _find_existing_booking(session, idempotency_key)
    if existing is not None:
        approval_result = await session.execute(
            select(Approval).where(Approval.booking_id == existing.id)
        )
        approval = approval_result.scalar_one_or_none()
        return ProposeBookingResult(
            booking_id=str(existing.id),
            approval_id=str(approval.id) if approval else "",
            status=existing.status.value,
            idempotency_key=idempotency_key,
            was_existing=True,
        )

    option: FlightOption | HotelOption | CarRentalOption
    try:
        if isinstance(query, ProposeFlightBookingInput):
            option = FlightOption(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                provider="duffel",
                provider_offer_id=query.offer.offer_id,
                total_price_usd=query.offer.total_price_usd,
                cabin_class=query.offer.cabin_class.value,
                stops_outbound=query.offer.stops_outbound,
                segments=[segment.model_dump(mode="json") for segment in query.offer.segments],
                is_mock=False,
                is_selected=True,
            )
            session.add(option)
            await session.flush()

            booking = Booking(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                booking_type=booking_type,
                flight_option_id=option.id,
                hotel_option_id=None,
                car_rental_option_id=None,
                status=BookingStatus.PENDING_APPROVAL,
                idempotency_key=idempotency_key,
                total_price_usd=total_price_usd,
            )
        elif isinstance(query, ProposeHotelBookingInput):
            option = HotelOption(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                provider="duffel",
                provider_offer_id=query.listing.search_result_id,
                name=query.listing.hotel_name,
                total_price_usd=query.listing.estimated_price_total_usd,
                check_in=query.check_in,
                check_out=query.check_out,
                star_rating=query.listing.hotel_rating,
                is_mock=False,
                is_selected=True,
            )
            session.add(option)
            await session.flush()

            booking = Booking(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                booking_type=booking_type,
                flight_option_id=None,
                hotel_option_id=option.id,
                car_rental_option_id=None,
                status=BookingStatus.PENDING_APPROVAL,
                idempotency_key=idempotency_key,
                total_price_usd=total_price_usd,
            )
        else:
            option = CarRentalOption(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                provider="duffel",
                provider_rate_id=query.rate.rate_id,
                provider_quote_id=query.quote.quote_id,
                car_description=query.rate.car_description,
                supplier_name=query.rate.supplier_name,
                payment_type=query.quote.payment_type.value,
                total_price_usd=query.quote.total_price_usd,
                pickup_location_name=query.rate.pickup_location_name,
                dropoff_location_name=query.rate.dropoff_location_name,
                pickup_at=query.rate.pickup_at,
                dropoff_at=query.rate.dropoff_at,
                driver_details=query.driver.model_dump(mode="json"),
                is_mock=False,
                is_selected=True,
            )
            session.add(option)
            await session.flush()

            booking = Booking(
                id=uuid.uuid4(),
                trip_id=trip_uuid,
                booking_type=booking_type,
                flight_option_id=None,
                hotel_option_id=None,
                car_rental_option_id=option.id,
                status=BookingStatus.PENDING_APPROVAL,
                idempotency_key=idempotency_key,
                total_price_usd=total_price_usd,
            )

        session.add(booking)
        await session.flush()

        approval = Approval(
            id=uuid.uuid4(),
            booking_id=booking.id,
            decision=ApprovalDecision.PENDING,
        )
        session.add(approval)
        await session.flush()

        await _write_audit_log(
            session,
            trip_id=trip_uuid,
            booking_id=booking.id,
            event_type="booking.proposed",
            payload={
                "booking_type": booking_type.value,
                "provider_offer_id": provider_offer_id,
                "total_price_usd": total_price_usd,
                "idempotency_key": idempotency_key,
            },
        )

    except IntegrityError:
        # Race condition: another concurrent call inserted the same
        # idempotency_key between our SELECT check and our INSERT. Roll
        # back this attempt's partial writes and return the row the other
        # call created, rather than raising or double-booking.
        await session.rollback()
        existing = await _find_existing_booking(session, idempotency_key)
        if existing is None:
            # Should be unreachable — an IntegrityError on this constraint
            # means a row with this key exists — but fail loudly rather
            # than silently if it somehow is.
            return ToolError(
                tool_name="propose_booking",
                error_type="integrity_error",
                message="A database conflict occurred and the resulting row could not be located.",
                retryable=True,
            )
        approval_result = await session.execute(
            select(Approval).where(Approval.booking_id == existing.id)
        )
        approval = approval_result.scalar_one_or_none()
        return ProposeBookingResult(
            booking_id=str(existing.id),
            approval_id=str(approval.id) if approval else "",
            status=existing.status.value,
            idempotency_key=idempotency_key,
            was_existing=True,
        )

    return ProposeBookingResult(
        booking_id=str(booking.id),
        approval_id=str(approval.id),
        status=booking.status.value,
        idempotency_key=idempotency_key,
        was_existing=False,
    )
