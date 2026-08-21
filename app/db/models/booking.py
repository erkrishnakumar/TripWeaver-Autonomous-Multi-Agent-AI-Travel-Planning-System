"""
Booking model.

Represents a proposed or executed booking action.

A booking starts at PENDING_APPROVAL and must not move to BOOKED
without the explicit human approval flow.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import BookingStatus, BookingType
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.approval import Approval
    from app.db.models.car_rental_option import CarRentalOption
    from app.db.models.flight_option import FlightOption
    from app.db.models.hotel_option import HotelOption
    from app.db.models.trip import Trip


class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = uuid_pk()

    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "trips.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    booking_type: Mapped[BookingType] = mapped_column(
        Enum(
            BookingType,
            name="booking_type",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
    )

    flight_option_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "flight_options.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    hotel_option_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "hotel_options.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    car_rental_option_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "car_rental_options.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    status: Mapped[BookingStatus] = mapped_column(
        Enum(
            BookingStatus,
            name="booking_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=BookingStatus.PENDING_APPROVAL,
        nullable=False,
    )

    # Prevents duplicate real booking attempts.
    idempotency_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    total_price_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    provider_booking_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Set only after a real provider booking succeeds.",
    )

    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    trip: Mapped[Trip] = relationship(
        back_populates="bookings",
    )

    flight_option: Mapped[FlightOption | None] = relationship(
        back_populates="bookings",
    )

    hotel_option: Mapped[HotelOption | None] = relationship(
        back_populates="bookings",
    )

    car_rental_option: Mapped[CarRentalOption | None] = relationship(
        back_populates="bookings",
    )

    approval: Mapped[Approval | None] = relationship(
        back_populates="booking",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_bookings_idempotency_key",
        ),
        Index(
            "ix_bookings_trip_id",
            "trip_id",
        ),
        Index(
            "ix_bookings_status",
            "status",
        ),
    )
