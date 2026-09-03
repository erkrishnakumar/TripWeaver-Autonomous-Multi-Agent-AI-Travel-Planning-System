"""
Car rental option model.

Represents a candidate car rental discovered by the Researcher agent —
either a raw rate (estimate) or, once get_car_rental_quote() has been
called, a firmed-up quote. provider_quote_id is nullable because a rate
can be proposed for booking before a quote has been fetched, same "estimate
now, firm later" caveat HotelOption already carries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk, variant_json

if TYPE_CHECKING:
    from app.db.models.booking import Booking
    from app.db.models.trip import Trip


class CarRentalOption(Base, TimestampMixin):
    __tablename__ = "car_rental_options"

    id: Mapped[uuid.UUID] = uuid_pk()

    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "trips.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="duffel",
    )

    provider_rate_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    provider_quote_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Set only once get_car_rental_quote() has firmed up a price for this rate.",
    )

    car_description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    supplier_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    payment_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="One of Duffel's Cars payment types: prepaid, guarantee, postpaid.",
    )

    total_price_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    pickup_location_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    dropoff_location_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    pickup_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    dropoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    driver_details: Mapped[dict[str, Any] | None] = mapped_column(
        variant_json,
        nullable=True,
        doc="Driver PII (given_name, family_name, date_of_birth, email, phone_number) — "
        "set only when this option is proposed for booking, never before.",
    )

    is_mock: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    is_selected: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    trip: Mapped[Trip] = relationship(
        back_populates="car_rental_options",
    )

    bookings: Mapped[list[Booking]] = relationship(
        back_populates="car_rental_option",
    )

    __table_args__ = (
        Index(
            "ix_car_rental_options_trip_price",
            "trip_id",
            "total_price_usd",
        ),
        Index(
            "ix_car_rental_options_selected",
            "is_selected",
        ),
    )
