"""
FastAPI app — Phase 8. Starts with the smallest correct slice: a health
check and a real, read-only GET /trips/{id}, proving the DB wiring works
end to end before tackling POST /trips or the approval endpoints (both
need a real design decision first — how a long-running CrewAI Flow that
currently blocks on a synchronous CLI input() gets triggered and later
resumed over HTTP — not something to improvise here).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, get_db
from app.api.rate_limit import limiter
from app.api.schemas import (
    ApprovalDecisionResponse,
    AuditLogEntryRead,
    BookingRead,
    ConfirmApprovalRequest,
    ConfirmInfoResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RegisterRequest,
    RejectApprovalRequest,
    ResetPasswordRequest,
    TokenResponse,
    TripCreate,
    TripProceedResponse,
    TripRead,
    UserRead,
)
from app.auth.passwords import PasswordTooLongError, hash_password, verify_password
from app.auth.reset_tokens import generate_reset_token, hash_reset_token
from app.auth.tokens import create_access_token
from app.config import settings
from app.db.models import Approval, AuditLog, Booking, FlightOption, PasswordResetToken, Trip, User
from app.db.models.enums import BookingType, TripStatus
from app.email.send_email import EmailSendError, send_password_reset_email
from app.logging_config import configure_logging
from app.tools.confirm_booking import confirm_booking, reject_booking
from app.tools.create_trip import create_trip
from app.tools.flights import get_flight_offer
from app.tools.schemas import ToolError
from app.worker.tasks import propose_trip_bookings, run_trip_planning

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="TripWeaver API")

app.state.limiter = limiter


def _handle_rate_limit_exceeded(request: Request, exc: Exception) -> Response:
    """Thin adapter around slowapi's own handler -- Starlette's
    add_exception_handler() expects a (Request, Exception) -> Response
    signature, but slowapi's handler is typed narrower ((Request,
    RateLimitExceeded) -> Response), a real mypy --strict mismatch against
    the stub, not a runtime one: Starlette guarantees this handler is only
    ever invoked for a RateLimitExceeded (that's the whole point of
    registering it against that specific exception type), so the isinstance
    check below is a formality that satisfies mypy without changing
    behavior."""
    assert isinstance(exc, RateLimitExceeded)
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, _handle_rate_limit_exceeded)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/register", response_model=UserRead, status_code=201)
@limiter.limit("10/minute")
async def register(request: Request, body: RegisterRequest, db: DbSession) -> User:
    """Deliberately open self-registration, not invite-only -- Duffel's
    "closed user group" requirement (see docs/Auth_Requirement.md) means
    every request must come from an authenticated, identifiable user, not
    that account creation itself must be gated. Every OTHER endpoint below
    requires a valid token; this and /auth/login are the only public ones."""
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    try:
        hashed = hash_password(body.password)
    except PasswordTooLongError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    user = User(id=uuid.uuid4(), email=body.email, hashed_password=hashed, full_name=body.full_name)
    db.add(user)
    await db.commit()
    return user


@app.post("/auth/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, db: DbSession) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    invalid_credentials = HTTPException(status_code=401, detail="Incorrect email or password.")
    if user is None or not verify_password(body.password, user.hashed_password):
        raise invalid_credentials
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")
    return TokenResponse(access_token=create_access_token(str(user.id)))


@app.get("/auth/me", response_model=UserRead)
async def get_me(current_user: CurrentUser) -> User:
    return current_user


_FORGOT_PASSWORD_GENERIC_MESSAGE = (
    "If an account with that email exists, a password reset link has been sent."
)


@app.post("/auth/forgot-password", response_model=ForgotPasswordResponse)
@limiter.limit("5/hour")
async def forgot_password(
    request: Request, body: ForgotPasswordRequest, db: DbSession
) -> ForgotPasswordResponse:
    """Issues a single-use, expiring reset token and emails it via Resend
    (app/email/send_email.py). Rate-limited tighter than every other auth
    endpoint (5/hour, not the usual 10/minute) since each call costs a
    real email send through Resend's own quota -- this is the one auth
    endpoint where abuse has a real, metered cost attached, not just
    unwanted load. Returns the SAME generic message whether or not the
    email matched a real account, and never reveals whether the
    email actually sent successfully -- both are real user-enumeration
    vectors otherwise (a different message, or a different HTTP status on
    a Resend outage, would let a caller learn which emails have accounts).

    Falls back to returning reset_token directly in the response ONLY when
    RESEND_API_KEY isn't configured at all (local dev without a Resend
    account set up) -- see ForgotPasswordResponse's own docstring for why
    that fallback is explicitly dev-mode-only and unsafe to rely on for
    any real deployment."""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None:
        return ForgotPasswordResponse(message=_FORGOT_PASSWORD_GENERIC_MESSAGE)

    raw_token = generate_reset_token()
    reset_token = PasswordResetToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_reset_token(raw_token),
        expires_at=datetime.now(UTC)
        + timedelta(minutes=settings.password_reset_token_expire_minutes),
    )
    db.add(reset_token)
    await db.commit()
    logger.info("Password reset token issued for user %s", user.id)

    if not settings.resend_api_key:
        return ForgotPasswordResponse(
            message=_FORGOT_PASSWORD_GENERIC_MESSAGE, reset_token=raw_token
        )

    try:
        send_password_reset_email(user.email, raw_token)
    except EmailSendError:
        logger.exception("Failed to send password reset email for user %s", user.id)
        # Still the generic message -- see this function's own docstring.
    return ForgotPasswordResponse(message=_FORGOT_PASSWORD_GENERIC_MESSAGE)


@app.post("/auth/reset-password", response_model=UserRead)
@limiter.limit("10/minute")
async def reset_password(request: Request, body: ResetPasswordRequest, db: DbSession) -> User:
    invalid_token = HTTPException(
        status_code=400, detail="This reset token is invalid, expired, or already used."
    )
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_reset_token(body.token)
        )
    )
    reset_token = result.scalar_one_or_none()
    if reset_token is None or reset_token.used_at is not None:
        raise invalid_token
    # expires_at is always written tz-aware (UTC), but SQLite (used by
    # tests; the real DB is Postgres) doesn't reliably preserve tzinfo on
    # read-back the way Postgres does -- verified live, the same class of
    # naive-vs-aware bug as Approval.decided_at earlier in this project.
    # Normalize rather than assume: a naive value read back is treated as
    # UTC, matching how every timestamp in this project is always written.
    expires_at = reset_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise invalid_token

    user = await db.get(User, reset_token.user_id)
    if user is None:
        raise invalid_token

    try:
        user.hashed_password = hash_password(body.new_password)
    except PasswordTooLongError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    reset_token.used_at = datetime.now(UTC)
    await db.commit()
    logger.info("Password reset completed for user %s", user.id)
    return user


def _trip_or_404(trip: Trip | None, current_user: User) -> Trip:
    """A trip that exists but belongs to someone else returns the same 404
    as a trip that doesn't exist at all -- never confirm to a caller that a
    given trip_id belongs to another user."""
    if trip is None or trip.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip


@app.get("/trips/{trip_id}", response_model=TripRead)
async def get_trip(trip_id: uuid.UUID, db: DbSession, current_user: CurrentUser) -> Trip:
    trip = await db.get(Trip, trip_id)
    return _trip_or_404(trip, current_user)


@app.post("/trips", response_model=TripRead, status_code=201)
async def create_trip_endpoint(body: TripCreate, db: DbSession, current_user: CurrentUser) -> Trip:
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
        user_id=current_user.id,
    )
    await db.commit()
    run_trip_planning.delay(str(trip.id))
    logger.info(
        "Trip created (%s -> %s), enqueued for research/planning",
        trip.origin_iata,
        trip.destination_iata,
        extra={"trip_id": trip.id},
    )
    return trip


@app.get("/trips/{trip_id}/bookings", response_model=list[BookingRead])
async def list_trip_bookings(
    trip_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> list[BookingRead]:
    """What a human approver actually looks at: every Booking proposed for
    this trip, with its Approval decision inlined, so approval_id (needed
    for POST /approvals/{id}/confirm|reject) never has to be dug out of the
    database directly."""
    trip = await db.get(Trip, trip_id)
    _trip_or_404(trip, current_user)

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


@app.get(
    "/trips/{trip_id}/bookings/{booking_id}/confirm-info",
    response_model=ConfirmInfoResponse,
)
async def get_booking_confirm_info(
    trip_id: uuid.UUID, booking_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> ConfirmInfoResponse:
    """Closes the gap GET /trips/{id}/bookings can't: the real ids needed
    to fill out POST /approvals/{id}/confirm's body, without a raw DB
    script. For a flight booking, passenger_ids are LIVE re-fetched from
    Duffel (get_flight_offer -- the same hallucination-guard re-fetch
    confirm_booking() itself does before ever booking), never read from a
    possibly-stale cached value. Hotel bookings need no provider ids at
    all (guests are freeform names); car bookings can't be confirmed yet
    (see docs/Car_Rental_Payment_Gap.md)."""
    trip = await db.get(Trip, trip_id)
    _trip_or_404(trip, current_user)

    booking = await db.get(Booking, booking_id)
    if booking is None or booking.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="Booking not found")

    approval_result = await db.execute(select(Approval).where(Approval.booking_id == booking.id))
    approval = approval_result.scalar_one()  # propose_booking() always creates one

    if booking.booking_type == BookingType.FLIGHT:
        flight_option = await db.get(FlightOption, booking.flight_option_id)
        assert flight_option is not None  # guaranteed by the FK for a FLIGHT booking
        offer = get_flight_offer(flight_option.provider_offer_id)
        if isinstance(offer, ToolError):
            raise HTTPException(
                status_code=422, detail=f"Could not refresh this offer: {offer.message}"
            )
        return ConfirmInfoResponse(
            booking_type=booking.booking_type,
            approval_id=approval.id,
            passenger_ids=offer.passenger_ids,
            note=(
                "Use these passenger_ids verbatim as passengers[].passenger_id in "
                "POST /approvals/{approval_id}/confirm."
            ),
        )

    if booking.booking_type == BookingType.HOTEL:
        return ConfirmInfoResponse(
            booking_type=booking.booking_type,
            approval_id=approval.id,
            note=(
                "No provider ids needed -- supply guests[]/contact_email/"
                "contact_phone_number directly in POST /approvals/{approval_id}/confirm."
            ),
        )

    return ConfirmInfoResponse(
        booking_type=booking.booking_type,
        approval_id=approval.id,
        note="Car rental bookings cannot be confirmed yet -- see docs/Car_Rental_Payment_Gap.md.",
    )


@app.get("/trips/{trip_id}/audit-log", response_model=list[AuditLogEntryRead])
async def get_trip_audit_log(
    trip_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> list[AuditLog]:
    """A trip's full event history -- research_started/completed/failed,
    budget_checked, booking.proposed/confirmed/rejected/failed, etc. This
    data has always existed (app/tools/audit.py's log_stage_event() has
    written it since Phase 8's Celery work began); this is the first place
    it's exposed over HTTP rather than requiring a raw DB query, closing
    the same class of gap GET /trips/{id}/bookings closed for approvals."""
    trip = await db.get(Trip, trip_id)
    _trip_or_404(trip, current_user)

    result = await db.execute(
        select(AuditLog).where(AuditLog.trip_id == trip_id).order_by(AuditLog.sequence)
    )
    return list(result.scalars().all())


@app.post("/trips/{trip_id}/proceed", response_model=TripProceedResponse)
async def proceed_with_trip(
    trip_id: uuid.UUID, db: DbSession, current_user: CurrentUser
) -> TripProceedResponse:
    trip = _trip_or_404(await db.get(Trip, trip_id), current_user)
    if trip.status != TripStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Trip is not awaiting approval (current status: {trip.status.value})",
        )
    propose_trip_bookings.delay(str(trip_id))
    logger.info("Gate 1: human approved, proposing bookings", extra={"trip_id": trip_id})
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


async def _require_own_approval(
    db: AsyncSession, approval_id: uuid.UUID, current_user: User
) -> uuid.UUID:
    """Same not-found-not-forbidden discipline as _trip_or_404, one join
    deeper: Approval -> Booking -> Trip.user_id. Deliberately checked here
    at the API layer, not inside confirm_booking()/reject_booking()
    themselves -- those stay auth-agnostic and reusable (e.g. from a future
    CLI/admin tool with no concept of "the current HTTP user"), same
    separation of concerns as propose_booking() never importing anything
    from app/api/. Returns the owning trip's id so callers can tag their
    own log lines with it -- confirm/reject only ever receive an
    approval_id, not a trip_id, otherwise."""
    result = await db.execute(
        select(Trip.id, Trip.user_id)
        .join(Booking, Booking.trip_id == Trip.id)
        .join(Approval, Approval.booking_id == Booking.id)
        .where(Approval.id == approval_id)
    )
    row = result.one_or_none()
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Approval not found")
    return uuid.UUID(str(row.id))


@app.post("/approvals/{approval_id}/confirm", response_model=ApprovalDecisionResponse)
async def confirm_approval(
    approval_id: uuid.UUID,
    body: ConfirmApprovalRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ApprovalDecisionResponse:
    """Gate 2. THE ONLY endpoint in this project ever allowed to trigger a
    real provider booking -- see app/tools/confirm_booking.py's module
    docstring for the full contract, including why a provider-side failure
    comes back as a normal 200 (booking_status="booking_failed") rather
    than an HTTP error: the human's approval succeeded, Duffel's booking
    call is a separate, honestly-reported outcome."""
    trip_id = await _require_own_approval(db, approval_id, current_user)
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
        logger.warning("Gate 2 confirm rejected: %s", result.message, extra={"trip_id": trip_id})
        _raise_for_tool_error(result)
    logger.info(
        "Gate 2: confirm -> %s (ref=%s)",
        result.booking_status,
        result.provider_booking_reference,
        extra={"trip_id": trip_id},
    )
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
    db: DbSession,
    current_user: CurrentUser,
) -> ApprovalDecisionResponse:
    trip_id = await _require_own_approval(db, approval_id, current_user)
    result = await reject_booking(
        db,
        str(approval_id),
        decided_by=body.decided_by,
        decision_notes=body.decision_notes,
    )
    if isinstance(result, ToolError):
        logger.warning("Gate 2 reject call failed: %s", result.message, extra={"trip_id": trip_id})
        _raise_for_tool_error(result)
    logger.info("Gate 2: human rejected the proposed booking", extra={"trip_id": trip_id})
    return ApprovalDecisionResponse(
        booking_id=uuid.UUID(result.booking_id),
        approval_id=uuid.UUID(result.approval_id),
        booking_status=result.booking_status,
        provider_booking_reference=result.provider_booking_reference,
        message=result.message,
    )
