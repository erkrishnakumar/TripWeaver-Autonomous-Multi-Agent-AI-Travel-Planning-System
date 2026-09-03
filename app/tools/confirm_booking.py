"""
confirm_booking() / reject_booking() — Gate 2, the ONLY code path in this
project ever allowed to call a real provider booking endpoint.

Mirrors propose_booking.py's discipline (DB-backed, async, one Booking +
one Approval row per call) with one deliberate difference: unlike
propose_booking(), confirm_booking() performs a REAL, IRREVERSIBLE external
side effect (an actual Duffel order/booking) partway through. Because of
that, this module commits internally right after updating Booking/Approval
status, rather than leaving the commit to the caller the way propose_
booking() does — if a caller-owned commit failed after a real booking had
already succeeded at the provider, the DB would show PENDING_APPROVAL for
something that is, in reality, already booked. Committing here closes that
window as tightly as possible.

Before ever calling a real booking endpoint, this re-fetches the offer/rate
by its stored provider id (get_flight_offer()/get_hotel_rate()+get_hotel_
quote()) rather than trusting whatever price was cached in FlightOption/
HotelOption at propose time -- the same hallucination-guard/freshness
discipline flow.py already applies before propose_booking(), extended here
to also catch an offer that has simply expired between proposal and human
approval (which can be minutes or days later).

Car rentals: confirming one requires a real, tokenized card payment (Duffel
Cars has no "bill to balance" option at all, unlike Flights/Stays) -- see
docs/Car_Rental_Payment_Gap.md for the full history. The caller must supply
a `three_d_secure_session_id`, obtained via Duffel's browser-side card-
tokenization flow (create_component_client_key() issues the client_key
that flow needs) -- a raw card number never reaches this backend at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, Booking, CarRentalOption, FlightOption, HotelOption
from app.db.models.enums import ApprovalDecision, BookingStatus, BookingType
from app.tools.audit import log_stage_event
from app.tools.car_rentals import create_car_rental_booking, get_car_rental_quote
from app.tools.flights import create_flight_order, get_flight_offer
from app.tools.hotels import create_hotel_booking, get_hotel_quote, get_hotel_rate
from app.tools.schemas import (
    BookingDecisionResult,
    CarQuoteInput,
    DriverDetails,
    HotelGuestDetails,
    PassengerDetails,
    ToolError,
)


async def _load_approval_and_booking(
    session: AsyncSession, approval_id: str
) -> tuple[Approval, Booking] | ToolError:
    try:
        approval_uuid = uuid.UUID(approval_id)
    except ValueError:
        return ToolError(
            tool_name="confirm_booking",
            error_type="invalid_approval_id",
            message=f"'{approval_id}' is not a valid UUID.",
            retryable=False,
        )

    approval = await session.get(Approval, approval_uuid)
    if approval is None:
        return ToolError(
            tool_name="confirm_booking",
            error_type="approval_not_found",
            message=f"No approval found with id '{approval_id}'.",
            retryable=False,
        )

    if approval.decision != ApprovalDecision.PENDING:
        return ToolError(
            tool_name="confirm_booking",
            error_type="already_decided",
            message=f"Approval '{approval_id}' was already {approval.decision.value}.",
            retryable=False,
        )

    booking = await session.get(Booking, approval.booking_id)
    if booking is None:
        # Unreachable under normal operation -- Approval.booking_id is a
        # NOT NULL FK -- but fail loudly rather than silently if it happens.
        return ToolError(
            tool_name="confirm_booking",
            error_type="booking_not_found",
            message=f"Approval '{approval_id}' references a booking that no longer exists.",
            retryable=False,
        )

    if booking.status != BookingStatus.PENDING_APPROVAL:
        return ToolError(
            tool_name="confirm_booking",
            error_type="booking_not_pending",
            message=(
                f"Booking '{booking.id}' is not pending approval "
                f"(current status: {booking.status.value})."
            ),
            retryable=False,
        )

    return approval, booking


async def reject_booking(
    session: AsyncSession,
    approval_id: str,
    *,
    decided_by: str | None = None,
    decision_notes: str | None = None,
) -> BookingDecisionResult | ToolError:
    """Rejects a pending approval. No provider call is ever made here."""
    loaded = await _load_approval_and_booking(session, approval_id)
    if isinstance(loaded, ToolError):
        return loaded
    approval, booking = loaded

    approval.decision = ApprovalDecision.REJECTED
    approval.decided_by = decided_by
    approval.decided_at = datetime.now(UTC)
    approval.decision_notes = decision_notes
    booking.status = BookingStatus.REJECTED

    await log_stage_event(
        session,
        str(booking.trip_id),
        "booking.rejected",
        payload={"booking_id": str(booking.id), "decided_by": decided_by},
        booking_id=str(booking.id),
    )
    await session.commit()

    return BookingDecisionResult(
        booking_id=str(booking.id),
        approval_id=str(approval.id),
        booking_status=booking.status.value,
        message="Booking rejected.",
    )


async def confirm_booking(
    session: AsyncSession,
    approval_id: str,
    *,
    passengers: list[PassengerDetails] | None = None,
    guests: list[HotelGuestDetails] | None = None,
    contact_email: str | None = None,
    contact_phone_number: str | None = None,
    three_d_secure_session_id: str | None = None,
    decided_by: str | None = None,
) -> BookingDecisionResult | ToolError:
    """
    Confirms a pending approval and actually books it for real against
    Duffel. See module docstring for the re-verification/commit-timing
    rationale.

    passengers is required (and must exactly match the freshly re-fetched
    offer's passenger_ids) for a FLIGHT booking; guests/contact_email/
    contact_phone_number are required for a HOTEL booking;
    three_d_secure_session_id is required for a CAR booking (see module
    docstring). None of this is collected or persisted anywhere before
    this call -- this is the first and only point real passenger/guest PII
    enters the system, and it is never persisted beyond what create_flight_
    order()/create_hotel_booking()/create_car_rental_booking() themselves
    send to Duffel.

    A real provider failure (e.g. an expired offer, a declined payment)
    does NOT raise -- it sets Booking.status = BOOKING_FAILED with
    failure_reason set, and returns that as a normal (non-exceptional)
    result, matching this project's "fail honestly" principle: a failed
    provider call is never disguised as a successful booking.
    """
    loaded = await _load_approval_and_booking(session, approval_id)
    if isinstance(loaded, ToolError):
        return loaded
    approval, booking = loaded

    if booking.booking_type == BookingType.CAR:
        if not three_d_secure_session_id:
            return ToolError(
                tool_name="confirm_booking",
                error_type="missing_payment_token",
                message="three_d_secure_session_id is required to confirm a car rental booking.",
                retryable=False,
            )
        car_option = await session.get(CarRentalOption, booking.car_rental_option_id)
        assert car_option is not None  # guaranteed by the FK for a CAR booking

        # Re-quote fresh, same freshness discipline as flights/hotels above
        # -- a quote can expire between propose time and this human
        # approval, which can be minutes or days later.
        fresh_quote = get_car_rental_quote(CarQuoteInput(rate_id=car_option.provider_rate_id))
        if isinstance(fresh_quote, ToolError):
            return await _fail_booking(session, booking, approval, fresh_quote.message, decided_by)

        driver = DriverDetails.model_validate(car_option.driver_details)
        car_booking = create_car_rental_booking(
            fresh_quote.quote_id, driver, three_d_secure_session_id
        )
        if isinstance(car_booking, ToolError):
            return await _fail_booking(session, booking, approval, car_booking.message, decided_by)

        reference = str(
            car_booking.get("reference")
            or car_booking.get("booking_reference")
            or car_booking.get("id", "")
        )
        return await _succeed_booking(session, booking, approval, reference, decided_by)

    if booking.booking_type == BookingType.FLIGHT:
        if not passengers:
            return ToolError(
                tool_name="confirm_booking",
                error_type="missing_passengers",
                message="passengers is required to confirm a flight booking.",
                retryable=False,
            )
        flight_option = await session.get(FlightOption, booking.flight_option_id)
        assert flight_option is not None  # guaranteed by the FK for a FLIGHT booking

        offer = get_flight_offer(flight_option.provider_offer_id)
        if isinstance(offer, ToolError):
            return await _fail_booking(session, booking, approval, offer.message, decided_by)

        order = create_flight_order(offer, passengers)
        if isinstance(order, ToolError):
            return await _fail_booking(session, booking, approval, order.message, decided_by)

        reference = str(
            order.get("booking_reference") or order.get("reference") or order.get("id", "")
        )
        return await _succeed_booking(session, booking, approval, reference, decided_by)

    # HOTEL
    if not guests or not contact_email or not contact_phone_number:
        return ToolError(
            tool_name="confirm_booking",
            error_type="missing_guest_details",
            message=(
                "guests, contact_email, and contact_phone_number are all required to "
                "confirm a hotel booking."
            ),
            retryable=False,
        )
    hotel_option = await session.get(HotelOption, booking.hotel_option_id)
    assert hotel_option is not None  # guaranteed by the FK for a HOTEL booking

    nights = max((hotel_option.check_out - hotel_option.check_in).days, 1)
    fresh_rate = get_hotel_rate(hotel_option.provider_offer_id, nights)
    if isinstance(fresh_rate, ToolError):
        return await _fail_booking(session, booking, approval, fresh_rate.message, decided_by)
    if fresh_rate.rate_id is None:
        return await _fail_booking(
            session,
            booking,
            approval,
            f"Search result '{hotel_option.provider_offer_id}' has no bookable rate.",
            decided_by,
        )

    quote = get_hotel_quote(fresh_rate.rate_id)
    if isinstance(quote, ToolError):
        return await _fail_booking(session, booking, approval, quote.message, decided_by)

    stays_booking = create_hotel_booking(
        quote.quote_id, contact_email, contact_phone_number, guests
    )
    if isinstance(stays_booking, ToolError):
        return await _fail_booking(session, booking, approval, stays_booking.message, decided_by)

    reference = str(
        stays_booking.get("reference")
        or stays_booking.get("booking_reference")
        or stays_booking.get("id", "")
    )
    return await _succeed_booking(session, booking, approval, reference, decided_by)


async def _succeed_booking(
    session: AsyncSession,
    booking: Booking,
    approval: Approval,
    provider_booking_reference: str,
    decided_by: str | None,
) -> BookingDecisionResult:
    booking.status = BookingStatus.BOOKED
    booking.provider_booking_reference = provider_booking_reference
    approval.decision = ApprovalDecision.APPROVED
    approval.decided_by = decided_by
    approval.decided_at = datetime.now(UTC)

    await log_stage_event(
        session,
        str(booking.trip_id),
        "booking.confirmed",
        payload={
            "booking_id": str(booking.id),
            "provider_booking_reference": provider_booking_reference,
        },
        booking_id=str(booking.id),
    )
    await session.commit()

    return BookingDecisionResult(
        booking_id=str(booking.id),
        approval_id=str(approval.id),
        booking_status=booking.status.value,
        provider_booking_reference=provider_booking_reference,
        message="Booking confirmed.",
    )


async def _fail_booking(
    session: AsyncSession,
    booking: Booking,
    approval: Approval,
    failure_reason: str,
    decided_by: str | None,
) -> BookingDecisionResult:
    """A real provider call was made and it failed -- this is NOT the same
    as a rejection (the human said yes; Duffel said no). The Approval stays
    APPROVED (the human's decision didn't change) while the Booking itself
    records the provider failure, so a human reviewing this trip can tell
    "approved but the provider declined it" apart from "human said no"."""
    booking.status = BookingStatus.BOOKING_FAILED
    booking.failure_reason = failure_reason
    approval.decision = ApprovalDecision.APPROVED
    approval.decided_by = decided_by
    approval.decided_at = datetime.now(UTC)

    await log_stage_event(
        session,
        str(booking.trip_id),
        "booking.failed",
        payload={"booking_id": str(booking.id), "failure_reason": failure_reason},
        booking_id=str(booking.id),
    )
    await session.commit()

    return BookingDecisionResult(
        booking_id=str(booking.id),
        approval_id=str(approval.id),
        booking_status=booking.status.value,
        message=f"Provider booking failed: {failure_reason}",
    )
